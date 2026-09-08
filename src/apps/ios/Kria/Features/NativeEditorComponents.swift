import AVKit
import Foundation
import SwiftUI

// The native editor deliberately keeps its chrome quiet. These components are
// small enough to be composed into one 390pt-wide surface and are also useful
// in previews and UI tests without depending on the chat workspace.

enum NativeEditorMode: String, CaseIterable, Identifiable {
    case chat = "Chat"
    case editor = "Editor"

    var id: String { rawValue }
}

enum NativeEditorTool: String, CaseIterable, Identifiable {
    case kria = "Kria"
    case text = "Text"
    case captions = "Captions"
    case visuals = "Visuals"
    case sounds = "Sounds"
    case overlays = "Overlays"
    case styles = "Styles"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .kria: "sparkles"
        case .text: "textformat"
        case .captions: "captions.bubble"
        case .visuals: "camera.filters"
        case .sounds: "waveform"
        case .overlays: "square.on.square"
        case .styles: "paintpalette"
        }
    }

    var accessibilityHint: String {
        switch self {
        case .kria: "Review editing proposals from Kria"
        case .text: "Add or edit text overlays"
        case .captions: "Turn captions on or off and choose a style"
        case .visuals: "Visual adjustments are not available yet"
        case .sounds: "Adjust the music volume"
        case .overlays: "Overlay cards are not available yet"
        case .styles: "Choose a text style preset"
        }
    }
}

struct NativeEditorTopBar: View {
    let projectTitle: String
    @Binding var mode: NativeEditorMode
    let onBack: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            Button(action: onBack) {
                Label("Projects", systemImage: "chevron.left")
                    .labelStyle(.titleAndIcon)
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(minWidth: 76, minHeight: 44, alignment: .leading)
            }
            .accessibilityIdentifier("native-editor-projects")
            .accessibilityHint("Return to your projects")

            Spacer(minLength: 4)

            Text(projectTitle)
                .font(KriaFont.body(15).weight(.semibold))
                .lineLimit(1)
                .frame(maxWidth: 90)
                .accessibilityIdentifier("native-editor-project-title")

            Spacer(minLength: 4)

            Text("E")
                .font(KriaFont.body(13).weight(.bold))
                .foregroundStyle(KriaColor.ink)
                .frame(width: 30, height: 30)
                .background(KriaColor.lime)
                .clipShape(Circle())
                .accessibilityLabel("Account")
                .accessibilityIdentifier("native-editor-account")

            Picker("Workspace", selection: $mode) {
                ForEach(NativeEditorMode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 110)
            .accessibilityIdentifier("native-editor-workspace-switcher")
        }
        .padding(.horizontal, 14)
        .frame(height: 54)
        .background(KriaColor.paper)
    }
}

struct NativeEditorHeaderRow: View {
    let title: String
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        HStack(spacing: 12) {
            Text(title)
                .font(KriaFont.display(20))
                .lineLimit(1)
                .accessibilityIdentifier("native-editor-context-title")

            Spacer()

            Button(action: session.undo) {
                Image(systemName: "arrow.uturn.backward")
                    .frame(width: 44, height: 44)
            }
            .disabled(!session.canUndo || session.isSaving)
            .accessibilityLabel("Undo")
            .accessibilityIdentifier("native-editor-undo")

            Button(action: session.redo) {
                Image(systemName: "arrow.uturn.forward")
                    .frame(width: 44, height: 44)
            }
            .disabled(!session.canRedo || session.isSaving)
            .accessibilityLabel("Redo")
            .accessibilityIdentifier("native-editor-redo")

            Button {
                Task { await session.save() }
            } label: {
                Text(session.isSaving ? "Saving…" : session.hasUnsavedChanges ? "Save" : "Saved")
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(session.hasUnsavedChanges ? KriaColor.limeText : KriaColor.zinc)
                    .padding(.horizontal, 11)
                    .frame(minHeight: 32)
                    .background(session.hasUnsavedChanges ? KriaColor.limeSoft : KriaColor.softZinc)
                    .clipShape(Capsule())
            }
            .disabled(session.isSaving || !session.hasUnsavedChanges)
            .accessibilityLabel(session.isSaving ? "Saving" : session.hasUnsavedChanges ? "Save changes" : "Saved")
            .accessibilityIdentifier("native-editor-save")
        }
        .padding(.horizontal, 14)
        .frame(height: 48)
        .background(KriaColor.paper)
    }
}

struct NativeEditorSaveBanner: View {
    @ObservedObject var session: NativeEditorSession

    @ViewBuilder var body: some View {
        switch session.saveState {
        case .conflict:
            banner(
                title: "A newer edit exists",
                detail: "Your changes are still here. Close and reopen the editor to reload before saving.",
                systemImage: "arrow.triangle.2.circlepath",
                tint: .orange
            )
        case .failed(let message):
            banner(title: "Couldn’t save this edit", detail: message, systemImage: "exclamationmark.triangle", tint: .red)
        case .previewPending:
            banner(
                title: "Saved — preview updating",
                detail: "Your edit is safe. The cloud preview is rendering now.",
                systemImage: "checkmark.circle",
                tint: KriaColor.limeText
            )
        default:
            EmptyView()
        }
    }

    private func banner(title: String, detail: String, systemImage: String, tint: Color) -> some View {
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
        .accessibilityIdentifier("native-editor-save-state")
    }
}

struct NativeEditorTransport: View {
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        HStack(spacing: 13) {
            Button(action: session.togglePlayback) {
                Image(systemName: session.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(KriaColor.paper)
                    .frame(width: 42, height: 42)
                    .background(KriaColor.ink)
                    .clipShape(Circle())
            }
            .accessibilityLabel(session.isPlaying ? "Pause preview" : "Play preview")
            .accessibilityIdentifier("native-editor-play-pause")

            Text(nativeTimecode(session.currentTime))
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(KriaColor.ink)
                .monospacedDigit()
                .accessibilityLabel("Current time \(nativeTimecode(session.currentTime))")

            GeometryReader { proxy in
                let width = max(proxy.size.width, 1)
                ZStack(alignment: .leading) {
                    Capsule().fill(KriaColor.line).frame(height: 4)
                    Capsule()
                        .fill(KriaColor.lime)
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
                    case .increment: session.seek(to: min(session.currentTime + increment, session.duration))
                    case .decrement: session.seek(to: max(session.currentTime - increment, 0))
                    @unknown default: break
                    }
                }
            }
            .frame(height: 44)

            Text(nativeTimecode(session.duration))
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(KriaColor.zinc)
                .monospacedDigit()
                .accessibilityLabel("Duration \(nativeTimecode(session.duration))")
        }
        .padding(.horizontal, 14)
        .frame(height: 54)
        .background(KriaColor.paper)
    }

    private var progress: CGFloat {
        guard session.duration > 0 else { return 0 }
        return min(max(session.currentTime / session.duration, 0), 1)
    }
}

struct NativeEditorTimeline: View {
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        VStack(spacing: 8) {
            HStack {
                Text("Drag the timeline to scrub")
                    .font(KriaFont.body(12).weight(.medium))
                    .foregroundStyle(KriaColor.zinc)
                Spacer()
            }

            NativeMiniStrip(session: session)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("native-editor-mini-strip")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
        .background(KriaColor.paper)
    }
}

struct NativeEditorContextStrip: View {
    @ObservedObject var session: NativeEditorSession
    let onAdjust: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Button(action: onAdjust) {
                Label("Adjust", systemImage: "slider.horizontal.3")
                    .frame(maxWidth: .infinity, minHeight: 42)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: true))
            .accessibilityIdentifier("native-editor-adjust")

            Button(action: onAdjust) {
                Label("Audio", systemImage: "speaker.slash")
                    .frame(maxWidth: .infinity, minHeight: 42)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: false))
            .accessibilityIdentifier("native-editor-clip-audio")

            Button(action: session.deleteSelectedClip) {
                Label("Delete", systemImage: "trash")
                    .frame(maxWidth: .infinity, minHeight: 42)
            }
            .buttonStyle(NativeEditorContextButtonStyle(isAccent: false))
            .disabled(!session.canEditTimeline || session.draft.clips.count <= 1)
            .accessibilityIdentifier("native-editor-delete")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 4)
        .background(KriaColor.paper)
    }
}

private struct NativeEditorContextButtonStyle: ButtonStyle {
    let isAccent: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(12).weight(.semibold))
            .foregroundStyle(isAccent ? KriaColor.limeText : KriaColor.ink)
            .background(isAccent ? KriaColor.limeSoft : KriaColor.softZinc)
            .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
            .opacity(configuration.isPressed ? 0.65 : 1)
    }
}

struct NativeEditorToolRail: View {
    @Binding var selected: NativeEditorTool?
    let onSelect: (NativeEditorTool) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 5) {
                ForEach(NativeEditorTool.allCases) { tool in
                    Button { selected = tool; onSelect(tool) } label: {
                        VStack(spacing: 5) {
                            Image(systemName: tool.icon)
                                .font(.system(size: 16, weight: .semibold))
                            Text(tool.rawValue)
                                .font(KriaFont.body(11).weight(.medium))
                                .lineLimit(1)
                        }
                        .foregroundStyle(selected == tool ? KriaColor.ink : KriaColor.zinc)
                        .frame(width: 67, height: 60)
                        .background(selected == tool ? KriaColor.limeSoft : KriaColor.paper)
                        .clipShape(RoundedRectangle(cornerRadius: 11, style: .continuous))
                    }
                    .accessibilityLabel(tool.rawValue)
                    .accessibilityHint(tool.accessibilityHint)
                    .accessibilityIdentifier("native-editor-tool-\(tool.rawValue.lowercased())")
                }
            }
            .padding(.horizontal, 10)
        }
        .frame(height: 72)
        .background(KriaColor.paper)
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
                    .foregroundStyle(KriaColor.limeText)
                    .frame(width: 42, height: 42)
                    .background(KriaColor.limeSoft)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                Text(title).font(KriaFont.display(25))
            }
            Text(reason)
                .font(KriaFont.body(15))
                .foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
            Label("You can keep editing the rest of the cut here.", systemImage: "checkmark.circle")
                .font(KriaFont.body(13).weight(.medium))
                .foregroundStyle(KriaColor.limeText)
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
