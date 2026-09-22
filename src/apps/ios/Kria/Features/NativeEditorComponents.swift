import AVKit
import Foundation
import SwiftUI

// The native editor deliberately keeps its chrome quiet. These components are
// small enough to be composed into one 390pt-wide surface and are also useful
// in previews and UI tests without depending on the chat workspace.

enum NativeEditorTool: String, CaseIterable, Identifiable {
    case kria = "Kria"
    case text = "Text"
    case captions = "Captions"
    case visuals = "Visuals"
    case sounds = "Sounds"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .kria: "sparkles"
        case .text: "textformat"
        case .captions: "captions.bubble"
        case .visuals: "camera.filters"
        case .sounds: "waveform"
        }
    }

    var accessibilityHint: String {
        switch self {
        case .kria: "Review editing proposals from Kria"
        case .text: "Add or edit text overlays"
        case .captions: "Turn captions on or off and choose a style"
        case .visuals: "Browse visual lanes and adjust supported effects"
        case .sounds: "Adjust the music volume"
        }
    }
}

struct NativeEditorTopBar: View {
    let onBack: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            Button(action: onBack) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("Back to chat")
            .accessibilityIdentifier("native-editor-back")
            Spacer()
        }
        .padding(.leading, 4)
        .padding(.trailing, 8)
        .frame(height: 44)
        .background(KriaColor.paper)
    }
}

struct NativeEditorSaveBanner: View {
    @ObservedObject var session: NativeEditorSession

    @ViewBuilder var body: some View {
        switch session.saveState {
        case .conflict:
            VStack(spacing: 0) {
                banner(
                    title: "A newer edit exists",
                    detail: "Your changes are still here. Reload the latest version, then review and save again.",
                    systemImage: "arrow.triangle.2.circlepath",
                    tint: .orange
                )
                Button("Reload latest and keep my edits") {
                    Task { await session.rebaseAfterConflict() }
                }
                .font(KriaFont.body(12).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(KriaColor.softZinc)
                .accessibilityIdentifier("native-editor-resolve-conflict")
            }
        case .failed(let message):
            banner(title: "Couldn’t save this edit", detail: message, systemImage: "exclamationmark.triangle", tint: .red)
        case .renderRetryNeeded(let message):
            VStack(spacing: 0) {
                banner(title: "Saved — render didn’t start", detail: message, systemImage: "arrow.clockwise", tint: .orange)
                Button("Retry render") { Task { await session.retryRender() } }
                    .font(KriaFont.body(12).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(KriaColor.softZinc)
                    .accessibilityIdentifier("native-editor-retry-render")
            }
        case .refreshFailed(let message):
            banner(title: "Couldn’t refresh this edit", detail: message, systemImage: "arrow.clockwise", tint: .orange)
        case .loadFailed(let message):
            banner(title: "Couldn’t load this edit", detail: message, systemImage: "exclamationmark.triangle", tint: .red)
        case .previewFailed(let message):
            VStack(spacing: 0) {
                banner(title: "Preview needs another check", detail: message, systemImage: "arrow.clockwise", tint: .orange)
                Button("Check preview again") { session.retryPreviewRefresh() }
                    .font(KriaFont.body(12).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(KriaColor.softZinc)
                    .accessibilityIdentifier("native-editor-retry-preview")
            }
        case .previewPending:
            banner(
                title: "Saved — preview updating",
                detail: session.rendersOnDevice
                    ? "Your edit is saved. Check rendering progress on this iPhone."
                    : "Your edit is safe. The cloud preview is rendering now.",
                systemImage: "checkmark.circle",
                tint: KriaColor.ink
            )
        default:
            EmptyView()
        }
    }

    private func banner(title: String, detail: String, systemImage: String, tint: Color) -> some View {
        NativeEditorBannerRow(title: title, detail: detail, systemImage: systemImage, tint: tint, identifier: "native-editor-save-state")
    }
}

/// Status row shared by the editor's save and export feedback.
struct NativeEditorBannerRow: View {
    let title: String
    let detail: String
    let systemImage: String
    let tint: Color
    let identifier: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: systemImage)
                .foregroundStyle(tint)
                .frame(width: 24, height: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(KriaFont.body(12).weight(.semibold))
                Text(detail).font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(KriaColor.softZinc)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(identifier)
    }
}

struct NativeEditorTransport: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject private var clock: NativeEditorPlaybackClock

    init(session: NativeEditorSession) {
        self.session = session
        _clock = ObservedObject(wrappedValue: session.playbackClock)
    }

    var body: some View {
        HStack(spacing: 13) {
            Button(action: session.togglePlayback) {
                Image(systemName: session.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(KriaColor.paper)
                    .frame(width: 44, height: 44)
                    .background(KriaColor.ink)
                    .clipShape(Circle())
            }
            .accessibilityLabel(session.isPlaying ? "Pause preview" : "Play preview")
            .accessibilityIdentifier("native-editor-play-pause")

            Text(nativeTimecode(clock.currentTime))
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(KriaColor.ink)
                .monospacedDigit()
                .accessibilityLabel("Current time")
                .accessibilityValue(nativeTimecode(clock.currentTime))
                .accessibilityIdentifier("native-editor-current-time")

            GeometryReader { proxy in
                let width = max(proxy.size.width, 1)
                ZStack(alignment: .leading) {
                    Capsule().fill(KriaColor.line).frame(height: 4)
                    Capsule()
                        .fill(KriaColor.sky)
                        .frame(width: width * progress, height: 4)
                }
                .frame(height: 44)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                    let fraction = min(max(value.location.x / width, 0), 1)
                    session.seek(to: max(session.duration, 0) * fraction)
                })
                .accessibilityElement()
                .accessibilityLabel("Preview position")
                .accessibilityValue("\(Int(progress * 100)) percent")
                .accessibilityAdjustableAction { direction in
                    let increment = max(session.duration, 1) / 20
                    switch direction {
                    case .increment: session.seek(to: min(clock.currentTime + increment, session.duration))
                    case .decrement: session.seek(to: max(clock.currentTime - increment, 0))
                    @unknown default: break
                    }
                }
            }
            .frame(height: 44)

            Text(nativeTimecode(session.duration))
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(KriaColor.zinc)
                .monospacedDigit()
                .accessibilityLabel("Duration")
                .accessibilityValue(nativeTimecode(session.duration))
                .accessibilityIdentifier("native-editor-duration")
        }
        .padding(.horizontal, 14)
        .frame(height: 54)
        .background(KriaColor.paper)
    }

    private var progress: CGFloat {
        guard session.duration > 0 else { return 0 }
        return min(max(clock.currentTime / session.duration, 0), 1)
    }
}

struct NativeEditorTimeline: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    var bottomClearance: CGFloat = 0
    var isCovered = false

    var body: some View {
        VStack(spacing: 6) {
            ForEach(session.pendingEditorImports.filter { $0.lane == .timeline }) { pending in
                HStack(spacing: 8) {
                    if pending.status == "failed" { Image(systemName: "exclamationmark.circle").foregroundStyle(KriaColor.failureText) }
                    else { ProgressView().controlSize(.small) }
                    Text(pending.status == "failed" ? (pending.error ?? "Couldn’t prepare media.") : "Preparing media import…")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    Spacer()
                    if pending.status == "failed" && pending.retryable {
                        Button("Retry") { Task { await session.retryPendingEditorImport(pending) } }.font(KriaFont.body(12)).frame(minHeight: 44)
                    }
                    Button("Remove", role: .destructive) { Task { await session.dismissPendingEditorImport(pending) } }.font(KriaFont.body(12)).frame(minHeight: 44)
                }
                .accessibilityIdentifier("native-editor-pending-timeline-import")
            }
            NativeMiniStrip(session: session, bottomClearance: bottomClearance, isCovered: isCovered)
                .frame(maxHeight: .infinity)
                .accessibilityIdentifier("native-editor-mini-strip")
        }
        .padding(.horizontal, 14)
        .padding(.top, 5)
        .background(KriaColor.paper)
    }
}

struct NativeEditorContextStrip: View {
    @ObservedObject var session: NativeEditorSession
    let onAdjust: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            Button(action: onAdjust) {
                Label("Adjust", systemImage: "slider.horizontal.3")
                    .frame(minHeight: 44)
                    .padding(.horizontal, 16)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: true))
            .accessibilityIdentifier("native-editor-adjust")

            Button(action: onAdjust) {
                Label("Audio", systemImage: "speaker.slash")
                    .frame(minHeight: 44)
                    .padding(.horizontal, 16)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: false))
            .accessibilityIdentifier("native-editor-clip-audio")

            Button(action: session.deleteSelectedClip) {
                Label("Delete", systemImage: "trash")
                    .frame(minHeight: 44)
                    .padding(.horizontal, 16)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: false))
            .disabled(!session.canEditTimeline || session.draft.clips.count <= 1)
            .accessibilityIdentifier("native-editor-delete")
        }
        .padding(4)
        .frame(height: NativeEditorIslandMetrics.contextHeight)
        .nativeEditorIslandSurface()
    }
}

struct NativeEditorTextContextStrip: View {
    let onEdit: () -> Void
    let onDeselect: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            Button(action: onEdit) {
                Label("Edit text", systemImage: "textformat")
                    .frame(minHeight: 44)
                    .padding(.horizontal, 16)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: true))
            .accessibilityIdentifier("native-editor-text-edit-action")
            Button(action: onDeselect) {
                Label("Deselect", systemImage: "xmark")
                    .frame(minHeight: 44)
                    .padding(.horizontal, 16)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: false))
        }
        .padding(4)
        .frame(height: NativeEditorIslandMetrics.contextHeight)
        .accessibilityElement(children: .contain)
        // See the note in `NativeEditorIslandSurface.glass` — the identifier
        // has to live on a plain marker, not directly on the glass-surfaced
        // view, or its reported frame silently loses its padding.
        .background(
            Color.clear
                .accessibilityElement()
                // The marker is its own VoiceOver stop, so it reads as the
                // group's heading rather than an unlabeled element.
                .accessibilityLabel("Text actions")
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("native-editor-text-context")
        )
        .nativeEditorIslandSurface()
    }
}

private struct NativeEditorContextButtonStyle: ButtonStyle {
    let isAccent: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(12).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .background(isAccent ? KriaColor.sage : Color.clear, in: Capsule())
            .opacity(configuration.isPressed ? 0.65 : 1)
    }
}

struct NativeEditorToolRail: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Namespace private var selectionNamespace
    let selected: NativeEditorTool?
    var availableWidth: CGFloat = 328
    var connected = false
    let onSelect: (NativeEditorTool) -> Void

    private var shouldReduceMotion: Bool {
        reduceMotion || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    var body: some View {
        HStack(spacing: NativeEditorIslandMetrics.toolSpacing) {
            ForEach(NativeEditorTool.allCases.filter { $0 != .kria }) { tool in
                Button { onSelect(tool) } label: {
                    VStack(spacing: 4) {
                        Image(systemName: tool.icon)
                            .font(.system(size: 17, weight: .medium))
                        Text(tool.rawValue)
                            .font(KriaFont.body(11).weight(selected == tool ? .semibold : .medium))
                            .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                            .lineLimit(1)
                            // The island pins each tool to a fixed width, so
                            // larger Dynamic Type shrinks the label instead
                            // of truncating it.
                            .minimumScaleFactor(0.7)
                    }
                    .foregroundStyle(selected == tool ? KriaColor.ink : KriaColor.zinc)
                    .frame(width: min(NativeEditorIslandMetrics.toolWidth, max(44, (availableWidth - 22) / 4)), height: NativeEditorIslandMetrics.toolHeight)
                    .background {
                        if selected == tool {
                            if shouldReduceMotion {
                                Capsule().fill(KriaColor.ink.opacity(0.07))
                            } else {
                                Capsule().fill(KriaColor.ink.opacity(0.07))
                                    .matchedGeometryEffect(id: "native-editor-tool-selection", in: selectionNamespace)
                            }
                        }
                    }
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(tool.rawValue)
                .accessibilityHint(selected == tool ? "Close this panel" : tool.accessibilityHint)
                .accessibilityAddTraits(selected == tool ? .isSelected : [])
                .accessibilityIdentifier("native-editor-tool-\(tool.rawValue.lowercased())")
            }
        }
        .padding(.horizontal, NativeEditorIslandMetrics.islandHorizontalPadding)
        .padding(.vertical, NativeEditorIslandMetrics.islandVerticalPadding)
        .frame(height: NativeEditorIslandMetrics.islandHeight)
        .accessibilityElement(children: .contain)
        // `.glassEffect()` (used by `.nativeEditorIslandSurface()` on iOS
        // 26+) corrupts the accessibility/hit-test frame of any ancestor
        // that carries `.accessibilityElement(children: .contain)` or a
        // bare `.accessibilityIdentifier` in its subtree: the reported
        // frame silently collapses to the union of only the "real"
        // (non-glass) accessible children, dropping this view's own
        // padding. Confirmed by isolating every other modifier here one at
        // a time; only removing `.glassEffect()` fixed it. Rather than drop
        // Liquid Glass, the identifier lives on a plain, non-glass marker
        // leaf added *after* `.contain` seals the button group, so its
        // frame reflects the padded capsule correctly for both VoiceOver
        // and UI tests, while `.contain` above still groups the buttons.
        .background(
            Color.clear
                .accessibilityElement()
                .accessibilityLabel("Editing tools")
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("native-editor-tool-rail")
        )
        .nativeEditorIslandSurface(isEnabled: !connected)
        .animation(shouldReduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.86), value: selected)
    }
}

struct NativeEditorUnavailableView: View {
    let title: String
    let reason: String
    let systemImage: String

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                Image(systemName: systemImage)
                    .font(.title2)
                    .foregroundStyle(KriaColor.ink)
                    .frame(width: 42, height: 42)
                    .background(KriaColor.sage)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                Text(title).font(KriaFont.display(25))
            }
            Text(reason)
                .font(KriaFont.body(15))
                .foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
            Label("You can keep editing the rest of the cut here.", systemImage: "checkmark.circle")
                .font(KriaFont.body(13).weight(.medium))
                .foregroundStyle(KriaColor.ink)
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .presentationDetents([.height(250)])
    }
}

func nativeTimecode(_ seconds: TimeInterval) -> String {
    let safe = max(seconds, 0)
    let minutes = Int(safe) / 60
    let remainder = safe - Double(minutes * 60)
    return String(format: "%02d:%04.1f", minutes, remainder)
}
