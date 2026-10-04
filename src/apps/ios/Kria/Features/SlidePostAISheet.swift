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
/// sparkles entry) that opens this sheet. With `slide_post_chat_edit` on it drives the chat-edit flow:
/// each message stages an edited draft on the page behind the sheet (unsaved, undoable) and the sheet
/// keeps the transcript, change chips, retry, Undo and Save. With it off it is the explicit propose
/// flow: ask, review the proposal, Apply or Undo.
struct SlidePostAISheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var session: SlidePostSession
    let api: any KriaAPIClient
    let itemID: String?
    let chatEnabled: Bool
    let canRequestProposal: Bool
    let uploadGuidance: String?
    @State private var prompt = ""

    private static let noteFill = Color(red: 0xFD / 255, green: 0xF1 / 255, blue: 0xDC / 255)

    var body: some View {
        NavigationStack {
            Group { if chatEnabled { chatBody } else { proposeBody } }
                .background(KriaColor.paper)
                .toolbar {
                    // Chat edits stage on the page behind; Done is the obvious way back to it.
                    if chatEnabled {
                        ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.accessibilityIdentifier("slidepost-ai-done") }
                    }
                }
                .navigationBarTitleDisplayMode(chatEnabled ? .inline : .automatic)
                // The visible "Kria" heading is gone; keep the sheet's VoiceOver context.
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Kria")
                .onAppear { prompt = session.instruction }
                .onChange(of: prompt) { _, value in session.instruction = value }
        }
    }

    // MARK: Propose (slide_post_chat_edit off)

    private var proposeBody: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    TextField("Describe the post", text: $prompt, axis: .vertical)
                        .accessibilityLabel("Describe the post")
                        .accessibilityIdentifier("slidepost-prompt")
                        .lineLimit(2...5).padding(12).overlay(RoundedRectangle(cornerRadius: 12).stroke(KriaColor.border))
                    Button(session.isBusy ? "Thinking…" : "Propose changes") { Task { await propose() } }
                        .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
                        .disabled(!canRequestProposal)
                        .accessibilityIdentifier("slidepost-ask")
                    if let uploadGuidance {
                        Text(uploadGuidance).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    }
                    if let proposal = session.proposal {
                        Text(proposal.summary).font(KriaFont.body(14))
                        Text(proposal.draft.caption)
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                        Button("Apply proposal") { Task { await apply() } }
                            .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
                            .accessibilityIdentifier("slidepost-apply")
                            .id("slidepost-apply")
                    }
                    if session.canUndo {
                        Button("Undo applied change") { Task { await undo() } }
                            .buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity).disabled(session.isBusy)
                    }
                    if let error = session.error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollDismissesKeyboard(.interactively)
            // The medium detent can't show a proposal below the prompt field; bring Apply into view.
            .onChange(of: session.proposal != nil) { _, hasProposal in
                if hasProposal { withAnimation { proxy.scrollTo("slidepost-apply", anchor: .bottom) } }
            }
        }
    }

    private func propose() async {
        guard canRequestProposal, let itemID else { return }
        let brief = prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Arrange these photos and videos into a cohesive post." : prompt
        session.instruction = brief
        await session.propose(api: api, itemID: itemID, instruction: brief)
    }
    private func apply() async { guard let itemID else { return }; await session.applyProposal(api: api, itemID: itemID); if session.error == nil && session.proposal == nil { dismiss() } }
    private func undo() async { guard let itemID else { return }; await session.undo(api: api, itemID: itemID) }

    // MARK: Chat edit (slide_post_chat_edit on)

    private var hasText: Bool { !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    private var canSend: Bool { hasText && !session.isBusy && !session.isChatting && session.draft != nil && itemID != nil }

    private var chatBody: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if session.chat.isEmpty && !session.isChatting {
                            Text("Tell Kria what to change: reorder the photos, add text, set a look or rewrite the caption.")
                                .font(KriaFont.body(15)).foregroundStyle(KriaColor.zinc)
                                .accessibilityIdentifier("slidepost-ai-empty")
                        }
                        ForEach(session.chat) { message in bubble(message) }
                        if session.isChatting {
                            HStack(spacing: 10) {
                                ProgressView()
                                Text("Kria is editing…").font(KriaFont.body(16)).foregroundStyle(KriaColor.zinc)
                            }
                            .accessibilityElement(children: .combine).accessibilityIdentifier("slidepost-ai-working")
                        }
                        if let error = session.error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: session.chat.count) { _, _ in withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
                .onChange(of: session.isChatting) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            statusRow
            composer
        }
    }

    private var statusRow: some View {
        HStack(spacing: 10) {
            if session.hasUnsavedChanges {
                HStack(spacing: 6) {
                    Circle().fill(SlidePostTone.warning).frame(width: 7, height: 7)
                    Text("Unsaved").font(KriaFont.body(15).weight(.semibold)).foregroundStyle(SlidePostTone.warning)
                }
                .padding(.horizontal, 12).frame(minHeight: 36).background(Self.noteFill, in: Capsule())
                .accessibilityElement(children: .combine).accessibilityIdentifier("slidepost-ai-unsaved")
            }
            Spacer(minLength: 4)
            Button("Undo") { session.undoEdit() }.buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                .disabled(!session.canUndoEdit || session.isChatting).accessibilityIdentifier("slidepost-ai-undo")
            Button("Save") { Task { if let itemID { await session.save(api: api, itemID: itemID) } } }.buttonStyle(KriaPrimaryButtonStyle())
                .disabled(!session.hasUnsavedChanges || session.isBusy || session.isChatting).accessibilityIdentifier("slidepost-ai-save")
        }
        .padding(.horizontal, 16).padding(.vertical, 8)
    }

    private var composer: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkle").font(.system(size: 17, weight: .regular)).foregroundStyle(KriaColor.mutedInk).padding(.leading, 6)
            TextField("Ask Kria to edit your slides…", text: $prompt)
                .font(KriaFont.body(16)).submitLabel(.send).onSubmit { if canSend { Task { await send() } } }
                .disabled(session.isChatting)
                .accessibilityIdentifier("slidepost-ai-input")
            Button { Task { await send() } } label: {
                Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold)).foregroundStyle(.white)
                    .frame(width: 40, height: 40).background(KriaColor.ink, in: Circle())
                    .opacity(canSend ? 1 : 0.4)
            }
            .disabled(!canSend).accessibilityLabel("Send to Kria").accessibilityIdentifier("slidepost-ai-send")
        }
        .padding(.leading, 12).padding(.trailing, 6).frame(minHeight: 52)
        .kriaFloatingSurface(Capsule())
        .padding(.horizontal, 16).padding(.bottom, 8)
    }

    /// The edit stages on the page behind the sheet; this only clears the field and sends.
    private func send() async {
        guard canSend, let itemID else { return }
        let text = prompt
        // Validate first: a rejected message (too long) keeps its text and shows the error.
        guard session.canChat(message: text) else { return }
        prompt = ""; session.instruction = ""
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        await session.chatEdit(api: api, itemID: itemID, message: text)
    }

    @ViewBuilder private func bubble(_ message: SlidePostChatMessage) -> some View {
        if message.isUser {
            Text(message.text).font(KriaFont.body(16)).foregroundStyle(.white)
                .padding(.horizontal, 16).padding(.vertical, 12)
                .background(KriaColor.ink, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                .frame(maxWidth: .infinity, alignment: .trailing).padding(.leading, 56)
                .accessibilityIdentifier("slidepost-ai-user")
        } else {
            VStack(alignment: .leading, spacing: 10) {
                Text(message.text).font(KriaFont.body(16)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("slidepost-ai-reply")
                if !message.changes.isEmpty {
                    SlidePostWrap {
                        ForEach(message.changes, id: \.self) { change in
                            let note = SlidePostChatMessage.isNote(change)
                            Text(change).font(KriaFont.body(14).weight(.semibold))
                                .foregroundStyle(note ? SlidePostTone.warning : KriaColor.success)
                                .padding(.horizontal, 12).frame(minHeight: 32)
                                .background(note ? Self.noteFill : KriaColor.successSoft, in: Capsule())
                                .accessibilityIdentifier(note ? "slidepost-ai-note" : "slidepost-ai-change")
                        }
                    }
                }
                if message.retryText != nil {
                    Button("Try again") { if let itemID { Task { await session.retryChat(api: api, itemID: itemID, bubble: message) } } }
                        .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                        .accessibilityIdentifier("slidepost-ai-retry")
                }
            }
        }
    }
}
