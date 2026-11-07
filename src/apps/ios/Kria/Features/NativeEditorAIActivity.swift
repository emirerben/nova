import Foundation
import SwiftUI

/// A projection of the workspace's current request, never an independent work timer.
struct NativeEditorAIActivity: Equatable {
    let projectID: UUID
    let requestID: String
    let status: NativeEditorAIStatus

    static func current(projectID: UUID, isSending: Bool, sendingRequestID: String?,
                        isThinking: Bool, thinkingRequestID: String?) -> Self? {
        if isSending, let sendingRequestID {
            return Self(projectID: projectID, requestID: sendingRequestID, status: .sending)
        }
        if isThinking, let thinkingRequestID {
            return Self(projectID: projectID, requestID: thinkingRequestID, status: .thinking)
        }
        return nil
    }
}

enum NativeEditorAIStatus: Equatable {
    case sending, thinking, preparingPreview, rendering, syncing

    var title: String {
        switch self {
        case .sending: "Sending your message…"
        case .thinking: "Kria is working…"
        case .preparingPreview: "Preparing preview…"
        case .rendering: "Preparing your video…"
        case .syncing: "Syncing your video…"
        }
    }

    static func current(projectID: UUID, conversation: NativeEditorAIActivity?,
                        refresh: NativeEditorRefreshSync = .idle,
                        preview: NativeSourcePreviewState = .ready,
                        previewIsUpdating: Bool = false,
                        render: DeviceRenderPresentation? = nil) -> Self? {
        if case .targetLost = refresh { return nil }
        if let conversation, conversation.projectID == projectID { return conversation.status }
        if previewIsUpdating, !preview.isFailure { return .preparingPreview }
        // The displayed job can be finished while a different, newer job is still being prepared.
        if refresh == .newVersionRendering { return .rendering }
        if let render {
            // Waiting, errors and cancellation already have their own actionable surface.
            guard render.waiting == nil else { return nil }
            switch render.phase {
            case .preparing, .rendering: return .rendering
            case .syncing: return .syncing
            case .localReady, .synced, .needsAttention, .cancelled, .superseded: return nil
            }
        }
        return nil
    }
}

/// Only this small decorative leaf animates. Removing it stops the loop without a callback into
/// the editor, player or request state. Both accessibility settings use the static branch.
struct NativeEditorAIHalo: View {
    let animates: Bool

    var body: some View {
        Group {
            if animates {
                TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
                    let seconds = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 8)
                    edge(angle: seconds / 8 * 360)
                        // Keyboard and sheet layout must move the outline with its control.
                        .transaction { $0.animation = nil }
                }
            } else {
                Capsule().strokeBorder(KriaColor.zinc, lineWidth: 1.5)
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func edge(angle: Double) -> some View {
        let colors = [KriaColor.sky, KriaColor.lilac, KriaColor.butter, KriaColor.sky]
        let gradient = AngularGradient(colors: colors, center: .center,
                                       startAngle: .degrees(angle), endAngle: .degrees(angle + 360))
        return ZStack {
            Capsule().strokeBorder(gradient, lineWidth: 3).blur(radius: 4).opacity(0.7)
            Capsule().strokeBorder(gradient, lineWidth: 1.5)
        }
    }
}
