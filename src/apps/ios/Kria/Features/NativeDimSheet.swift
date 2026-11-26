import SwiftUI
import UIKit

/// Darkens the selected clip or photo evenly, 0 to 100% (100% is black). Text, captions, stickers and drawings stay
/// bright because the engine dims only the clip's picture. Every change goes through `NativeEditorSession.setClipDim`, so
/// the preview updates live and Save sends the row's `dim`; Apply to all sets the same strength on every clip.
struct NativeDimSheet: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    @State private var applyAllNote: String?

    static let step = 5

    private var percent: Int { session.clipDim(for: selection)?.percent ?? 0 }
    private var blockedReason: String? { session.dimBlockedReason(for: selection) }
    private var isBlocked: Bool { blockedReason != nil }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let reason = blockedReason {
                    Label(reason, systemImage: "lock")
                        .font(KriaFont.body(14))
                        .foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("native-editor-dim-blocked")
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text(Self.readout(percent))
                        .font(KriaFont.headline(34))
                        .foregroundStyle(KriaColor.ink)
                        .accessibilityIdentifier("native-editor-dim-value")
                    Text("Darkens the picture. Text, captions and stickers stay bright.")
                        .font(KriaFont.body(14))
                        .foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                slider
                HStack(spacing: 8) {
                    pill(String(localized: "Less dim"), systemImage: "minus", id: "native-editor-dim-less", iconOnly: true, enabled: percent > 0) {
                        set(percent - Self.step)
                    }
                    pill(String(localized: "More dim"), systemImage: "plus", id: "native-editor-dim-more", iconOnly: true, enabled: percent < 100) {
                        set(percent + Self.step)
                    }
                    Spacer(minLength: 0)
                    pill(String(localized: "Remove", comment: "Button: take the dim off this clip"), systemImage: nil, id: "native-editor-dim-remove",
                         enabled: percent > 0, blockable: false) {
                        session.setClipDim(nil, for: selection)
                        applyAllNote = nil
                    }
                }
                if session.timelineClips.count > 1 {
                    pill(String(localized: "Apply to all"), systemImage: nil, id: "native-editor-dim-apply-all") {
                        let dim = ClipDim(percent: percent)
                        applyAllNote = Self.applyAllNote(percent: dim == nil ? 0 : percent, clips: session.applyClipDimToAllClips(dim))
                    }
                    if let applyAllNote {
                        Text(applyAllNote)
                            .font(KriaFont.body(13))
                            .foregroundStyle(KriaColor.mutedInk)
                            .accessibilityIdentifier("native-editor-dim-apply-all-note")
                    }
                }
            }
            .padding(20)
        }
        .background(KriaColor.paper)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-dim-sheet")
    }

    private var slider: some View {
        let binding = Binding<Double>(get: { Double(percent) }, set: { set(Int($0.rounded())) })
        return Slider(value: binding, in: 0...100, step: 1) { editing in
            editing ? session.beginTransaction() : session.endTransaction()
        }
        .tint(KriaColor.ink)
        .frame(minHeight: 44)
        .disabled(isBlocked)
        .accessibilityLabel("Dim")
        .accessibilityValue(Self.readout(percent))
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: set(percent + Self.step)
            case .decrement: set(percent - Self.step)
            @unknown default: break
            }
        }
        .accessibilityIdentifier("native-editor-dim-slider")
    }

    private func set(_ next: Int) {
        let clamped = min(max(next, 0), 100)
        guard clamped != percent else { return }
        session.setClipDim(ClipDim(percent: clamped), for: selection)
    }

    private func pill(_ title: String, systemImage: String?, id: String, iconOnly: Bool = false, enabled: Bool = true,
                      blockable: Bool = true, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Group {
                if let systemImage, iconOnly { Image(systemName: systemImage) }
                else { Text(title) }
            }
            .font(KriaFont.body(15).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(minWidth: iconOnly ? 44 : 0, minHeight: 44)
            .padding(.horizontal, iconOnly ? 6 : 16)
            .background(KriaColor.fill, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(!enabled || (isBlocked && blockable))
        .accessibilityLabel(title)
        .accessibilityIdentifier(id)
    }

    static func readout(_ percent: Int) -> String { String(localized: "\(percent)%", comment: "How dark a clip is drawn, e.g. 40%") }

    static func applyAllNote(percent: Int, clips: Int) -> String {
        percent == 0
            ? String(localized: "Removed the dim from \(clips) clips.")
            : String(localized: "Set \(percent)% on \(clips) clips.")
    }
}
