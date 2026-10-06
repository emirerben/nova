import SwiftUI
import UIKit

/// Wraps its children onto new lines (the change chips).
private struct SlidePostWrap: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(proposal.width ?? 320, subviews).size
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = arrange(bounds.width, subviews)
        for (index, origin) in result.origins.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y), proposal: .unspecified)
        }
    }
    private func arrange(_ width: CGFloat, _ subviews: Subviews) -> (size: CGSize, origins: [CGPoint]) {
        var origins: [CGPoint] = [], x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            origins.append(CGPoint(x: x, y: y)); x += size.width + spacing; rowHeight = max(rowHeight, size.height); maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowHeight), origins)
    }
}

/// The ONLY place the slide editor talks to Kria. The page keeps a single AI button (the editor's
/// sparkles entry) that opens this sheet, and the sheet IS the video editor's Kria sheet: the same
/// `ChatConversationScroll` + `ChatMessageRow` + `ThinkingRow` + `ChatComposer`, fed by the slide
/// post's own thread. With `slide_post_chat_edit` on, each message stages an edited draft on the page
/// behind the sheet (unsaved, undoable); with it off (or not loaded) a message asks Kria to propose an
/// arrangement, shown inline with Apply and Undo.
struct SlidePostAISheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var session: SlidePostSession
    let api: any KriaAPIClient
    let itemID: String?
    let chatEnabled: Bool
    let canRequestProposal: Bool
    let uploadGuidance: String?
    @State private var prompt = ""
    @FocusState private var composerFocused: Bool

    var body: some View {
        ChatConversationScroll(isLoaded: true, updateToken: updateToken, scrollRequest: 0, dismissKeyboard: { composerFocused = false }) {
            conversation
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ChatComposer(
                text: $prompt, isSending: session.isBusy || session.isChatting, canAttach: false,
                blocksSubmission: !chatEnabled && !canRequestProposal,
                placeholder: chatEnabled ? "Ask Kria to edit your slides…" : "Describe the post…",
                isFocused: $composerFocused, attach: {}, send: { Task { await send() } }
            )
        }
        .background(KriaColor.paper)
        .onAppear { prompt = session.instruction }
        .onChange(of: prompt) { _, value in session.instruction = value }
    }

    private var updateToken: String {
        "\(session.chat.map { $0.id.uuidString }.joined(separator: "|"))|\(session.isChatting)|\(session.isBusy)|\(session.proposal != nil)"
    }

    // MARK: Conversation

    @ViewBuilder private var conversation: some View {
        if session.chat.isEmpty && !session.isChatting && session.proposal == nil {
            ChatMessageRow(message: .init(
                id: "slidepost-ai-intro", role: .assistant,
                content: chatEnabled
                    ? "Tell me what to change: reorder the photos, add text, set a look or rewrite the caption."
                    : "Tell me about the post and I'll propose an arrangement, cover and caption."
            ))
            .accessibilityIdentifier("slidepost-ai-empty")
        }
        ForEach(session.chat) { message in bubble(message) }
        if session.isChatting || (session.isBusy && !chatEnabled) { ThinkingRow().id("thinking") }
        if let proposal = session.proposal { proposalCard(proposal) }
        if let uploadGuidance { Text(uploadGuidance).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
        if let error = session.error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
    }

    @ViewBuilder private func bubble(_ message: SlidePostChatMessage) -> some View {
        if message.isUser {
            ChatMessageRow(message: .init(id: message.id.uuidString, role: .user, content: message.text))
        } else {
            VStack(alignment: .leading, spacing: 10) {
                ChatMessageRow(message: .init(id: message.id.uuidString, role: .assistant, content: message.text))
                if !message.changes.isEmpty {
                    SlidePostWrap {
                        ForEach(message.changes, id: \.self) { change in
                            let note = SlidePostChatMessage.isNote(change)
                            Text(change).font(KriaFont.body(12).weight(.semibold))
                                .foregroundStyle(note ? KriaColor.ink : KriaColor.success)
                                .padding(.horizontal, 12).frame(minHeight: 32)
                                .background(note ? KriaColor.butter : KriaColor.successSoft, in: Capsule())
                                .accessibilityIdentifier(note ? "slidepost-ai-note" : "slidepost-ai-change")
                        }
                    }
                }
                if message.retryText != nil {
                    Button("Try again") { if let itemID { Task { await session.retryChat(api: api, itemID: itemID, bubble: message) } } }
                        .buttonStyle(KriaPrimaryButtonStyle(fill: KriaColor.softZinc, minHeight: 44)).fixedSize()
                        .accessibilityIdentifier("slidepost-ai-retry")
                }
            }
        }
    }

    private func proposalCard(_ proposal: SlidePostProposal) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            ChatMessageRow(message: .init(id: "slidepost-ai-proposal", role: .assistant, content: proposal.summary, isProposal: true))
            if !proposal.draft.caption.isEmpty {
                Text(proposal.draft.caption).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            }
            Button("Apply proposal") { Task { await apply() } }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44)).fixedSize().disabled(session.isBusy)
                .accessibilityIdentifier("slidepost-apply")
        }
        .id("slidepost-apply")
    }

    // MARK: Actions

    private var hasText: Bool { !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func send() async {
        guard hasText, !session.isBusy, !session.isChatting, let itemID else { return }
        let text = prompt
        if chatEnabled {
            guard session.draft != nil, session.canChat(message: text) else { return }
            prompt = ""; session.instruction = ""
            composerFocused = false
            await session.chatEdit(api: api, itemID: itemID, message: text)
        } else {
            guard canRequestProposal else { return }
            session.instruction = text
            await session.propose(api: api, itemID: itemID, instruction: text)
        }
    }
    private func apply() async { guard let itemID else { return }; await session.applyProposal(api: api, itemID: itemID); if session.error == nil && session.proposal == nil { dismiss() } }
}
